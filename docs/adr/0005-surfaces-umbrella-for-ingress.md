# Surfaces umbrella: fold `channels/`, `app/`, `interactive/` into a single `omicsclaw/surfaces/` package

## Status

Accepted (2026-05-16).

## Context

ADR 0004 (2026-05-15) did a boundary-led restructure of the
``omicsclaw/`` package — splitting ``runtime/`` and ``core/`` by
responsibility and folding ``bot/`` away. After P4 landed, three
ingress entry points still sat as siblings at the package root
alongside ~18 other unrelated sub-packages:

    omicsclaw/
    ├── channels/      ← Channel Surface (10 IM adapters)
    ├── app/           ← Desktop Surface (FastAPI server)
    ├── interactive/   ← CLI Surface (REPL + TUI)
    ├── runtime/, skill/, providers/, services/, …  (15+ siblings)
    ├── cli.py         ← thin launcher shim
    ├── run_channels.py ← Channel Surface runner
    ├── setup_wizard.py ← interactive setup (rich + questionary)
    ├── __main__.py
    └── …

Two problems with this state:

1. **The three Surfaces are not visibly a single layer.** ``docs/CONTEXT.md``
   already names them formally — *Channel Surface*, *Desktop Surface*,
   *CLI Surface* — and notes that all three dispatch into
   ``core.llm_tool_loop``. But the package layout doesn't reflect
   that grouping. A new contributor reading ``ls omicsclaw/`` cannot
   see "this is the ingress layer"; the three Surfaces are scattered
   among 18 same-level peers (``runtime/``, ``skill/``, ``providers/``,
   ``services/``, ``engine/``, ``memory/``, …).

2. **Four entry scripts sit at the package root, two of them mis-named.**
   ``app/`` is a textbook ADR-0004 junk-drawer name (any Python project
   could call something ``app``). ``interactive/`` describes a behaviour
   ("interactive") rather than a Surface type — the SSE chat in ``app/``
   is also "interactive", the typing indicator in ``channels/`` is also
   "interactive". The word that distinguishes the Surface is *CLI*.

The reference for this restructure was the same CellClaw repo cited in
ADR 0004 — specifically its ingress story, where ``cli/``, ``web/``,
and ``gateway/`` sit as visibly-sibling top-level packages. The point
is *not* CellClaw's exact layout (ADR 0004 alternative #1 already
rejected that), but the **discoverability property**: opening any
ingress dir makes it obvious it's an ingress dir.

## Decision

Introduce ``omicsclaw/surfaces/`` as an umbrella package for the three
Surfaces, and fold the four scattered ingress scripts into it where
they belong. Single atomic commit, mirroring ADR 0004 P4 style.

The six design decisions in this restructure, in the order they were
resolved:

**Q1 — Scope: physical relocation only.** No new abstraction (no
``Surface`` base class, no shared dispatch contract). The three
Surfaces remain independent classes; only their parent directory
changes.

**Q2 — Umbrella name: ``surfaces/``.** Reuses the term already
defined in ``docs/CONTEXT.md`` (§"Surfaces"), so the language layer
needs no edits. Alternative names ``ingress/`` (introduces a new term
that would coexist redundantly with *Surface*) and *no umbrella*
(cellclaw-style sibling tops) were rejected — see below.

**Q3 — Sub-package names: ``{channels, desktop, cli}``.** Renames
``app/`` → ``desktop/`` and ``interactive/`` → ``cli/`` to align
paths with CONTEXT.md's three formal Surface names. ``channels/``
already matched and keeps its name.

**Q4 — Repo-root ``omicsclaw.py`` (1450 lines) stays at the repo
root.** It is a launcher script (not Python package code) and its
``OMICSCLAW_DIR = Path(__file__).resolve().parent`` derives
``SKILLS_DIR``, ``EXAMPLES_DIR``, ``SESSIONS_DIR``, ``DEFAULT_OUTPUT_ROOT``
from its on-disk location. Moving it into ``surfaces/cli/`` would
silently re-anchor those paths to ``omicsclaw/surfaces/cli/skills/``
etc. — an entire class of hidden bugs. The four scripts that *are*
inside the package move:

    omicsclaw/cli.py            -> omicsclaw/surfaces/cli/__init__.py (merged with prior interactive/__init__.py)
    omicsclaw/run_channels.py   -> omicsclaw/surfaces/channels/__main__.py
    omicsclaw/setup_wizard.py   -> omicsclaw/surfaces/cli/setup_wizard.py
    omicsclaw/__main__.py       (stays at package root per Python convention)

**Q5 — Break clean, no deprecation shims.** Two user-visible
contracts change:
- ``python -m omicsclaw.run_channels --channels telegram``
  → ``python -m omicsclaw.surfaces.channels --channels telegram``
- ``oc app-server`` → ``oc desktop-server``

Both are immediately removed; no transitional alias. Consistent
with ADR 0004 P4's "no half-finished migrations" precedent and
with ADR 0003's stance against dead fallback paths.

**Q6 — Single atomic commit.** Three Surfaces have no cross-imports
(``channels/`` does not import ``app/``; ``app/`` does not import
``interactive/``; etc., per CONTEXT.md §"Surfaces"), so per-Surface
phasing buys no verification value. Furthermore, the
``pyproject.toml`` entry-point updates (``oc``, ``oc-chat``,
``omicsclaw``, ``omicsclaw-chat`` all re-target) are inherently
cross-Surface — any phased intermediate commit would break the
``oc`` console script and be un-shippable.

## Consequences

**Wins**

- ``ls omicsclaw/`` now answers "what are the ingress paths?" with a
  single name (``surfaces/``). The three Surfaces sit together,
  visibly part of one layer.
- ``surfaces/{channels, desktop, cli}/`` names match CONTEXT.md's
  *Channel Surface*, *Desktop Surface*, *CLI Surface* — the
  language layer and the code layer agree.
- ``app/`` is gone — one less ADR-0004-junk-drawer name in the tree.
- ``run_channels.py`` becomes ``surfaces/channels/__main__.py``,
  which is both more discoverable (it sits next to the channels it
  runs) and idiomatically Python (``python -m`` on a package).
- The Makefile's ``bot-telegram`` / ``bot-feishu`` targets, which
  still pointed at the long-deleted ``python -m bot.run`` (ADR 0004
  P4 cleanup miss), are corrected in the same commit.

**Costs**

- ~316 Python import sites updated. Concentrated in test files
  (``tests/test_app_server.py``=81, ``tests/test_channels.py``=56,
  ``tests/test_notebook_files.py``=35), so the bulk is sed-able.
- ~82 docstring/comment references and ~48 test mock string paths
  (``monkeypatch.setattr("omicsclaw.app.server.X", …)``) need
  parallel updates.
- ~121 markdown documentation references (CLAUDE.md, docs/,
  README files, skills/) need path/command updates.
- ``pyproject.toml`` re-targets four entry points
  (``oc``/``oc-chat``/``omicsclaw``/``omicsclaw-chat``); the
  external command names do not change but their import paths do.
- ``graphify-out/`` anchors target files that move. A ``/graphify``
  re-index is required before knowledge-graph queries return
  correct citations (same caveat as ADR 0004).
- ``import X.Y as Z`` test-mock quirk (documented in ADR 0004 P4
  consequences) will recur for any test that mocks
  ``omicsclaw.app.server.X`` style attributes — needs the same
  ``monkeypatch.setattr(parent, "attr", fake, raising=False)``
  hardening pattern.

**Alternatives considered**

- **Surface base class + unified dispatch contract (Q1-B).** Would
  extract a ``Surface`` ABC with ``start()``/``stop()`` and route
  all three through one ``SurfaceManager``. Rejected: scope creep
  beyond the physical-relocation goal; the three Surfaces have
  legitimately different lifecycles (Channel = ``ChannelManager``
  multi-adapter, Desktop = FastAPI ``lifespan``, CLI = sync REPL),
  and forcing a shared base class would constrain each without a
  proven concrete benefit. Left as a possible future ADR if a real
  cross-Surface concern emerges.

- **No umbrella, cellclaw-style sibling top-level packages (Q2-C).**
  Would create ``omicsclaw/channels/``, ``omicsclaw/desktop/``,
  ``omicsclaw/cli/`` as three siblings under ``omicsclaw/``. Rejected
  because it directly contradicts ADR 0004 alternative #1, which one
  day prior rejected mirroring cellclaw's top-level layout. ADR 0004's
  reasoning (omicsclaw/ is the only top-level Python package; cellclaw
  parity has no engineering benefit beyond superficial naming) applies
  identically here. Choosing C would require either revoking ADR
  0004's stance or living with a doctrinal contradiction. The
  umbrella (``surfaces/``) achieves CellClaw-style discoverability
  *inside* the omicsclaw/ package boundary that ADR 0004 protects.

- **Keep current names (Q3-B), zero rename cost.** Would move
  ``app/`` and ``interactive/`` into ``surfaces/`` unchanged. Rejected
  because it preserves the two name problems the restructure exists
  to fix: ``app`` is a junk-drawer name (ADR 0004's main critique of
  ``core/``), and ``interactive`` describes a behaviour rather than
  the Surface type. CONTEXT.md would have to keep "Desktop Surface
  is in code called ``app``" as an explanatory footnote forever.

- **Move repo-root ``omicsclaw.py`` into ``surfaces/cli/main.py``
  (Q4-Y).** Would let ``surfaces/cli/`` contain 100% of the CLI logic
  instead of ~60%. Rejected because the 1450-line file is a launcher
  script, not package source, and its ``Path(__file__).parent``
  derives load-bearing data paths (``SKILLS_DIR``, ``EXAMPLES_DIR``,
  ``SESSIONS_DIR``, ``DEFAULT_OUTPUT_ROOT``). Moving it would silently
  re-anchor every default path to a sub-directory of the package, an
  entire class of hard-to-spot regressions. Q4-Y would also break the
  documented ``python omicsclaw.py …`` contract (used by CLAUDE.md
  ~30 times). Acceptable cost of Q4-X: ``surfaces/cli/`` shows the
  Surface scaffolding (REPL, TUI, setup wizard, console entry) but
  the skill-runner CLI itself lives one directory up.

- **Deprecation shims for the two broken entry points (Q5-Q).**
  Would leave ``omicsclaw/run_channels.py`` as a 5-line warning shim
  and add an ``app-server`` argparse alias that forwards to
  ``desktop-server``. Rejected: ADR 0004 P4 explicitly chose
  atomic-move over shims ("no half-finished migrations"). In a
  pre-1.0 single-developer project, shims rarely get cleaned up;
  ``grep`` for ``run_channels`` would keep returning results for
  years. Break-clean is cheaper at the project's current size.

- **Per-Surface phased commits (Q6-S).** Would split into three
  commits (one per Surface) plus a final pyproject/Makefile/docs
  commit. Rejected: the three Surfaces are import-independent, so
  three small tests carry no more information than one big test;
  and the pyproject re-target is inherently cross-Surface, meaning
  every phase-1 and phase-2 intermediate commit would have a broken
  ``oc`` console script and not be individually shippable. Atomic
  is simpler and gives the same coverage.

This ADR does *not* settle the broader question of whether the three
Surfaces should eventually share an abstraction (Q1-B). That decision
is deferred and will get its own ADR if and when a concrete shared
concern emerges (e.g. cross-Surface auth, unified per-request
namespace injection beyond what CONTEXT.md §"Surface namespace
defaults" already documents).

## Verification

Mirrors ADR 0004's verification protocol:

- Run the full test suite on this branch and on ``main`` after the
  restructure commit; compare failure sets via ``comm -23`` to
  confirm zero new failures.
- Spot-check ``oc desktop-server`` boots (the renamed CLI subcommand).
- Spot-check ``python -m omicsclaw.surfaces.channels --list`` (the
  new channel runner path).
- Re-index ``/graphify`` after the commit lands; verify a Surface-
  related query returns the new ``omicsclaw/surfaces/...`` anchor.

Two commits land in order:
1. ``docs(adr): record surfaces/ umbrella for ingress consolidation
   as ADR 0005`` (this file only).
2. ``refactor(surfaces): fold channels/app/interactive into surfaces/
   per ADR 0005`` (the atomic restructure; commit body references
   ADR-0005 by number).
